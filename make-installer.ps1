# make-installer.ps1 — one-shot CopyTrades installer build.
#
# Runs PyInstaller against CopyTrades.spec, then Inno Setup's iscc.exe
# against CopyTrades.iss, producing installer\CopyTradesSetup-<ver>.exe.
#
# Usage:
#     .\make-installer.ps1                  # full build
#     .\make-installer.ps1 -SkipPyInstaller # only re-compile the .iss (faster iteration)
#     .\make-installer.ps1 -Version 0.2.0   # stamp a different version into the installer
#
# Prerequisites:
#   - Python venv at .venv with PyInstaller + project deps installed.
#   - Inno Setup 6 installed. Default install path is auto-detected;
#     pass -IsccPath to override. Download: https://jrsoftware.org/isdl.php

[CmdletBinding()]
param(
    [string]$Version,
    [string]$IsccPath,
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---- locate iscc.exe -----------------------------------------------------
function Find-Iscc {
    param([string]$Override)
    if ($Override) {
        if (-not (Test-Path $Override)) { throw "iscc not found at $Override" }
        return $Override
    }
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    throw @"
Inno Setup 6 not found.

Install from https://jrsoftware.org/isdl.php (free), or pass:
    .\make-installer.ps1 -IsccPath 'C:\path\to\ISCC.exe'
"@
}

$iscc = Find-Iscc -Override $IsccPath
Write-Host "iscc: $iscc" -ForegroundColor DarkGray

# ---- ensure venv ---------------------------------------------------------
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Python venv missing at $venvPython. Run: python -m venv .venv && .venv\Scripts\pip install -e .[dev]"
}

# ---- kill any running app so PyInstaller can overwrite dist\ -------------
Get-Process -Name "CopyTrades" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# ---- pyinstaller ---------------------------------------------------------
if (-not $SkipPyInstaller) {
    if (Test-Path build) { Remove-Item build -Recurse -Force }
    if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }
    Write-Host "[1/2] Running PyInstaller..." -ForegroundColor Cyan
    & $venvPython -m PyInstaller --clean --noconfirm CopyTrades.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    if (-not (Test-Path "dist\CopyTrades\CopyTrades.exe")) {
        throw "PyInstaller succeeded but dist\CopyTrades\CopyTrades.exe is missing"
    }
} else {
    Write-Host "[1/2] Skipping PyInstaller (--SkipPyInstaller)" -ForegroundColor DarkYellow
    if (-not (Test-Path "dist\CopyTrades\CopyTrades.exe")) {
        throw "dist\CopyTrades\CopyTrades.exe missing — drop --SkipPyInstaller for the first build"
    }
}

# ---- optional version override -------------------------------------------
# Pass /DMyAppVersion=<x> to iscc so callers can stamp a release version
# without editing the .iss. The .iss's #define becomes a fallback only.
$isccArgs = @("CopyTrades.iss")
if ($Version) {
    $isccArgs += "/DMyAppVersion=$Version"
    Write-Host "Version override: $Version" -ForegroundColor DarkGray
}

# ---- iscc ----------------------------------------------------------------
Write-Host "[2/2] Compiling installer..." -ForegroundColor Cyan
if (-not (Test-Path installer)) { New-Item -ItemType Directory installer | Out-Null }
& $iscc @isccArgs
if ($LASTEXITCODE -ne 0) { throw "iscc failed (exit $LASTEXITCODE)" }

# ---- report --------------------------------------------------------------
$out = Get-ChildItem installer\CopyTradesSetup-*.exe |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $out) { throw "iscc reported success but no installer .exe found" }
$sizeMB = [math]::Round($out.Length / 1MB, 1)
Write-Host ""
Write-Host "Installer ready:" -ForegroundColor Green
Write-Host "  $($out.FullName)" -ForegroundColor Green
Write-Host "  $sizeMB MB" -ForegroundColor DarkGray
