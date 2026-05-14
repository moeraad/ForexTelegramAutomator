@echo off
REM Code-sign the built CopyTrades.exe and installer using SignTool.
REM
REM Usage:
REM   scripts\sign_exe.bat <path_to_cert.pfx> <pfx_password>
REM
REM Prerequisites:
REM   - Windows SDK (signtool.exe on PATH or under
REM     "C:\Program Files (x86)\Windows Kits\10\bin\<ver>\x64\signtool.exe")
REM   - A code-signing certificate:
REM       * Production: an EV certificate from DigiCert / Sectigo / etc (~$200/yr)
REM       * Dev/internal: create a self-signed cert via PowerShell:
REM           $c = New-SelfSignedCertificate -Type CodeSigning ^
REM                -Subject "CN=CopyTrades Dev" ^
REM                -CertStoreLocation Cert:\CurrentUser\My
REM           Export-PfxCertificate -Cert $c -FilePath devcert.pfx ^
REM                -Password (ConvertTo-SecureString -String "yourpass" -AsPlainText -Force)
REM         (Windows will still warn on a self-signed binary — only useful
REM         for internal trust chains where you've imported the cert.)

IF "%~1"=="" GOTO usage
IF "%~2"=="" GOTO usage

set CERT=%~1
set PASS=%~2
set TS=http://timestamp.digicert.com

where signtool >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo signtool not on PATH. Locate under Windows SDK and prepend its folder:
    echo   set PATH=C:\Program Files ^(x86^)\Windows Kits\10\bin\^<ver^>\x64;%%PATH%%
    exit /b 1
)

echo Signing CopyTrades.exe...
signtool sign /fd SHA256 /f "%CERT%" /p "%PASS%" /tr %TS% /td SHA256 "dist\CopyTrades\CopyTrades.exe"
IF %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo Signing installer (if present)...
FOR %%I IN (dist\CopyTrades-Setup-*.exe) DO (
    signtool sign /fd SHA256 /f "%CERT%" /p "%PASS%" /tr %TS% /td SHA256 "%%I"
)

echo Verifying...
signtool verify /pa "dist\CopyTrades\CopyTrades.exe"
FOR %%I IN (dist\CopyTrades-Setup-*.exe) DO signtool verify /pa "%%I"
exit /b 0

:usage
echo Usage: %~nx0 ^<cert.pfx^> ^<password^>
exit /b 1
